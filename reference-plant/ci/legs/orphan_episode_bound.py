#!/usr/bin/env python3
"""The orphan-episode-bound leg for the reference plant — the
consumer-side proof that one journaled `field_orphaned` stands for a
whole orphan episode (WW-ENG-003, WW-LCM-001).

The contract #1041's fix establishes is pinned on the rig and never on
the customer-owned pair: a tracking standby pinned on an
advancing-but-ownerless source must journal `field_orphaned` exactly
once for the contiguous episode — never once per ownerless apply —
must keep serving the `orphaned` verdict through the
evidence-free pull misses that interleave with its completed pulls
(never flickering to `degraded`), and those misses must still count
toward the armed failover budget. This leg is the mirror of the qa
rig's orphan-episode-bound leg (scenario 2160) under the file-discovered
convention, so the stage's diagnostics land on this file's
`orphan-episode-bound-*` stem and adding it edits no `check.sh`, the
boundary lint, or the harness.

The run:

- converges the manifest-declared pair — the field owner `active`, the
  declared standby `tracking` it — and gates the contract surface: the
  declared standby's `journal_file` persistence, the served
  checkpoint's `source_owns_field` ownership stamp, and a durable
  journal carrying `entry` records on an integer tick axis. Each
  absence is the pinned release predating the contract, never a
  violation of it;
- opens the episode: `POST /demote` on the field owner. The demoted
  member hands the field back and adopts the sibling's verified
  announced endpoint — the pair-keyed launch is what lets a member
  with no configured tracking source prove one — so each peer now
  tracks a peer that owns nothing and the declared standby pins
  `orphaned`;
- spawns the pinned probe: a third controller the leg launches as a
  tracking peer of the demoted owner, armed at a budget it declares
  and carrying its own `--journal-file`. The probe is the episode's
  observed peer — the one whose verdict and journal the leg audits —
  while the manifest-declared pair keeps the wiring and the arming the
  deployment declares. The declared `failover_budget` is the
  deployment's switchover bound and is far shorter than the episode
  the leg stages (an ownerless apply counts toward it exactly as a
  produced-nothing pull does), so the probe is sized past the whole
  window; its own arming is the leg's, never the deployment's;
- holds the episode across alternating rounds: the tracking source
  `SIGSTOP`-frozen (its monitor socket bound but unanswered) so the
  probe's in-flight pull drops to a produced-nothing miss, then
  `SIGCONT`-thawed so the advancing ownerless document lands again.
  Through every round the served verdict must stay `orphaned` — never
  `degraded` — the armed `failover` misses must advance
  monotonically, and the probe's served journal and declared durable
  file must each hold exactly **one** `field_orphaned` for the
  episode;
- closes the episode on a genuinely non-orphaned apply — the launch
  owner promoted back over the field it handed back, so the probe's
  next applies land `source_owns_field: true` and it reconverges
  `tracking` — then opens a second episode with a second demotion. The
  freshly orphaned run owes its own entry, so the bound is
  per-episode and not per-process;
- restores the launch layout: the field claimed back by the manifest's
  duty member with the declared standby `tracking` it and the probe
  behind it, verified through the served role reports after a settle
  train in the documented tracking-first order.

The contract postdates some pinned release lines: where the launched
tooling predates it — no declared journal file, a checkpoint without
the ownership stamp, a durable journal with no tick-axis records, a
demoted member refusing its demotion for want of a provable tracking
source, the island never forming, or the episode's one entry never
landing where the pull verdict itself proves the episode — the run's
evidence is the pre-contract shape and the leg reports
`orphan-episode-bound-digest inconclusive` rather than asserting.

Usage:

    orphan_episode_bound.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `orphan-episode-bound-digest <sha256>` line prints —
the check runs two passes and compares them
(`orphan-episode-bound-nondeterministic`). A contract violation
reports `orphan-episode-bound: …` lines on stderr and exits 1 — the
check's `orphan-episode-bound-failed`. `--tamper expect-flood`
doctors the leg's expectation to the defect shape — a `field_orphaned`
entry per ownerless pull, the journal flood the contract closed — so
the pass must fail naming the single entry it observed, proving the
bound assertion fires on the honest journal rather than passing an
unexercised contract.
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
import demote_reconvergence
import driver_recovery
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting a journaled `field_orphaned` per
# ownerless pull — the flood the contract closed — must surface the
# named diagnostic on the honest single-entry journal rather than
# passing an unexercised contract.
LEG = {
    # The next free slot in the stage's recorded order (870 is the
    # cause-alarm-quality leg); no two legs may share one.
    "order": 880,
    "title": "the orphan-episode-bound leg",
    "passes": "orphan-episode-bound-leg",
    "tampers": [
        {
            "name": "expect-flood",
            "passed": "an expect-flood case passed the "
            "orphan-episode-bound leg",
            "missed": "the expect-flood case did not report its "
            "named diagnostic",
            "evidence": [
                "the doctored expectation wanted a field_orphaned entry per ownerless pull",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a
    product failure. The first arg is the stable reason the
    `inconclusive` digest line prints (two identical passes must
    share it); the optional second arg the run's own evidence,
    reported on stderr only, where minted tokens and ephemeral
    endpoints belong."""


# The driven-scan bounds the episode's phases run. The declared pair
# converges `tracking` inside a few pulls; the pinned probe's orphaned
# applies pin within a handful of rounds; and the held window spans two
# freeze/thaw alternations — the produced-nothing misses and the
# completed ownerless pulls they interleave with — across one
# contiguous episode.
CONVERGE_SCANS = 8
PIN_SCANS = 12
HOLD_ROUNDS = 2
FROZEN_SCANS = 2
LIVE_SCANS = 2
EPISODE_TWO_SCANS = 12
SETTLE_SCANS = 6

# The pinned probe's armed budget's margin — `probe_budget` sizes the
# probe's arming from the manifest's declared `failover_budget`, and
# this is the slack left beyond every pull cycle the staging and the
# held window spend, so the promotion boundary is never what closes the
# window.
PROBE_MISS_HEADROOM = 4

# The doctor's stable evidence prefix — every failure the tampered
# pass records carries it, so the check's negative case finds it
# whether the honest run held the bound or a predating release
# offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted a field_orphaned entry per "
    "ownerless pull"
)


def freeze(process):
    """`SIGSTOP` on a spawned peer — the harness's stop/pause lever.
    The frozen process's monitor socket stays bound but unanswered,
    the connectable-but-silent endpoint the produced-nothing pull
    misses are made of. Without the signal there is no lever to stage
    the interleaved window, and the leg says so rather than passing
    vacuously."""
    if process is None or process.poll() is not None:
        raise Abort("the tracking source's process is not running")
    if not hasattr(signal, "SIGSTOP"):
        raise Abort("this platform has no SIGSTOP to freeze a peer with")
    process.send_signal(signal.SIGSTOP)


def thaw(process):
    """`SIGCONT` — the frozen peer serves and answers again, so the
    held ownerless document lands on the next pull."""
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGCONT)


def orphan_report(report):
    """Whether a served RoleReport is a standby pinned on the
    ownerless line — the episode's posture."""
    return (
        isinstance(report, dict)
        and report.get("role") == "standby"
        and claim_reclaim.sync_state(report) == "orphaned"
    )


def failover_evidence(report):
    """The armed heartbeat's served evidence — `(converged, misses,
    budget)` from a standby RoleReport, or None when the peer serves
    no failover section at all."""
    failover = (report or {}).get("failover")
    if not isinstance(failover, dict):
        return None
    return (
        failover.get("converged"),
        failover.get("misses"),
        failover.get("budget"),
    )


def orphan_entries(entries):
    """The `field_orphaned` records of a journal entry list —
    `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["field_orphaned"])
        for entry in entries
        if "field_orphaned" in entry.get("event", {})
    ]


def durable_orphans(path):
    """The `field_orphaned` record count in a `--journal-file` — the
    durable half of the bound, read whole because the durable record
    is append-only and the episode's floors are counts taken off the
    served tail."""
    return len(orphan_entries(stranded_rejoin.journal_entries(path)))


def episode_counts(served_tail, durable_path, floors):
    """The episode's `field_orphaned` counts on both journal surfaces
    since `floors`: the served tail's records and the durable file's,
    read in that order so the served read waits the durable sink's
    drain out and the file is caught up through the same tail."""
    served = len(orphan_entries(served_tail))
    return {
        "served": served - floors["served"],
        "durable": durable_orphans(durable_path) - floors["durable"],
    }


def observation(number, phase, report):
    """One normalized window row off the pinned probe's served role:
    the phase it was read in, the reported role, the sync verdict's
    variant, and the armed heartbeat's evidence as a stable list. The
    window's rows are the surface the verdict and miss-accounting
    clauses replay."""
    return {
        "round": number,
        "phase": phase,
        "role": report.get("role"),
        "sync": stranded_rejoin.sync_kind(report),
        "failover": list(failover_evidence(report) or ()),
    }


def watch_round(duty_url, probe_url, source_process, failures, evidence,
                rounds):
    """The held window: alternate the tracking source's freeze and
    thaw so produced-nothing pull misses interleave with completed
    ownerless pulls inside one episode. While the source is frozen its
    monitor is bound but silent, so only the pinned probe is driven —
    the harness's own requests to the frozen peer would block with no
    bound at all. Each observation reads the probe's served verdict and
    armed miss accounting. Returns `(window, budget_closed)` — the
    per-round observations and whether the armed budget's own promotion
    ended the window early."""
    window, budget_closed = [], False
    for number in range(rounds):
        freeze(source_process)
        try:
            for _ in range(FROZEN_SCANS):
                pair.scan(probe_url, failures)
                report = pair.get(f"{probe_url}/role", "GET /role", failures)
                window.append(observation(number, "frozen", report))
                if report.get("role") != "standby":
                    budget_closed = True
                    evidence["window"] = window
                    return window, budget_closed
        finally:
            thaw(source_process)
        for _ in range(LIVE_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(probe_url, failures)
            report = pair.get(f"{probe_url}/role", "GET /role", failures)
            window.append(observation(number, "live", report))
            if report.get("role") != "standby":
                budget_closed = True
                evidence["window"] = window
                return window, budget_closed
    evidence["window"] = window
    return window, budget_closed


def probe_budget(declared_budget):
    """The pinned probe's armed budget — the manifest's declared
    `failover_budget` plus every pull cycle the staging and the held
    window spend, with margin. The declared value is the deployment's
    switchover bound, far shorter than the episode the leg stages: an
    ownerless apply counts toward it exactly as a produced-nothing pull
    does, so a probe armed at the declared value would self-promote out
    of the window on its third pull and the alternation — the thing the
    bound is judged across — would never run. The probe is a leg-spawned
    third peer, not a declared pair member, so its arming is the leg's
    own to size; the declared pair keeps the arming the deployment
    declares."""
    return (
        declared_budget
        + PIN_SCANS
        + HOLD_ROUNDS * (FROZEN_SCANS + LIVE_SCANS)
        + PROBE_MISS_HEADROOM
    )


def spawn_pinned_probe(args, rig, duty_url, armed):
    """Spawn the pinned probe: a third controller the leg launches as a
    tracking peer of the demoted field owner, armed at `armed` and
    carrying its own `--journal-file` under the rig's scratch root. The
    probe is the episode's observed peer — the one whose orphan verdict
    and journal the leg audits. Returns
    `(process, url, files, preamble)`."""
    root = os.path.join(rig.scratch, "pinned-probe")
    os.makedirs(root, exist_ok=True)
    files = {"journal_file": os.path.join(root, "journal.jsonl")}
    process, url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        duty_url.removeprefix("http://"),
        files,
        auto_promote=armed,
        pair_token=pair.PAIR_TOKEN,
    )
    return process, url, files, preamble


def promoted(url, failures, what):
    """`POST /promote` answered by a promoting report — the documented
    switch the episode's close and the launch-layout restore are run
    through. Returns the report, or None when the promotion never
    answered."""
    status, report = pair.request(f"{url}/promote", {})
    if status == 200 and report.get("role") == "promoting":
        return report
    failures.append(
        f"POST /promote on {what} answered {status} {report} — the "
        "documented promotion must answer a promoting report"
    )
    return None


def orphan_episode_bound_pass(args, tamper):
    """The exercised run: converge the declared pair, gate the contract
    surface, open the orphan episode on the demoted pair's island with
    a pinned probe observing it, hold the episode across alternating
    produced-nothing misses and completed ownerless pulls, assert the
    single journaled entry on both surfaces with the verdict held and
    the misses counted, open the second episode, and restore the launch
    layout.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "orphan-episode-bound leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    declared_budget = standby_decl.get("failover_budget")
    if not isinstance(declared_budget, int) or declared_budget <= 0:
        raise Abort(
            f"{standby_decl['name']} declares no failover_budget — the "
            "armed heartbeat whose miss accounting the orphan episode "
            "counts against has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    probe = None
    duty_url = probe_url = None
    probe_files = None
    probe_path = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence on the manifest's declared wiring.
        converged = rig.converge(failures, CONVERGE_SCANS)
        evidence["converged"] = converged["ticks"][-1]
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "declared_budget": declared_budget,
            }
        )

        # Phase 2 — the contract surface: the declared standby's
        # durable journal persistence, the served checkpoint's
        # ownership stamp (the vocabulary the orphaned verdict is read
        # from), and a durable journal carrying tick-axis records.
        # Each absence is the pinned release predating the contract.
        if rig.standby_files.get("journal_file") is None:
            raise Inconclusive(
                f"{standby_decl['name']} declares no journal_file — "
                "the durable half of the episode's bound audit is "
                "absent"
            )
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["checkpoint"] = doc
        if "source_owns_field" not in doc:
            raise Inconclusive(
                "the tracking peer serves a checkpoint without the "
                "field-ownership stamp — the pinned release predates "
                "the ownerless-source vocabulary the orphaned verdict "
                f"is read from: {doc}"
            )
        durable = stranded_rejoin.durable_kinds(
            rig.standby_files["journal_file"]
        )
        if not any(
            isinstance(entry.get("tick"), int)
            and not isinstance(entry.get("tick"), bool)
            for entry in stranded_rejoin.journal_entries(
                rig.standby_files["journal_file"]
            )
        ):
            raise Inconclusive(
                f"{standby_decl['name']}'s durable journal carries no "
                "tick-axis records — the pinned release predates the "
                "attributed durable record the episode's own entries "
                "are counted on"
            )
        digest_entries.append(
            {
                "phase": "gate",
                "journal": "declared",
                "stamps": "present",
                "axis": "stamped",
                "durable": sorted(durable),
            }
        )

        # Phase 3 — the episode: `POST /demote` on the field owner.
        # The demoted member hands the field back and adopts the
        # sibling's verified announced endpoint — the pair-keyed launch
        # is what lets a member with no configured tracking source
        # prove one — so each peer tracks a peer owning nothing, and
        # the declared standby pins `orphaned` with it.
        demote_status, demote_report = pair.request(f"{duty_url}/demote", {})
        evidence["demote"] = {
            "status": demote_status,
            "body": demote_report,
        }
        if demote_status != 200 or demote_report.get("role") != "demoting":
            raise Inconclusive(
                "the field owner refused its demotion for want of a "
                "tracking source it could prove — the launched "
                "tooling predates the announced-source adoption the "
                "orphan episode's island is staged on",
                f"POST /demote answered {demote_status} {demote_report}",
            )
        adopted = demote_reconvergence.adopted_sources(
            rig.duty_files["journal_file"]
        )
        evidence["adopted"] = len(adopted)
        if not adopted:
            raise Inconclusive(
                "the demoted member journaled no tracking_source_adopted "
                "record — the launched tooling predates the attributed "
                "pull-target adoption the orphan episode's island is "
                "staged on",
                f"its journal kinds were {sorted(durable)}",
            )
        # The pinned probe: the episode's observed peer, launched as a
        # tracking peer of the demoted owner so the ownerless document
        # it serves is what the probe pins.
        probe, probe_url, probe_files, preamble = spawn_pinned_probe(
            args, rig, duty_url, probe_budget(declared_budget)
        )
        if probe_url is None:
            failures.append(
                "the pinned probe exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        probe_path = probe_files["journal_file"]
        floors = {"served": 0, "durable": 0}
        islanded = None
        for _ in range(PIN_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            pair.scan(probe_url, failures)
            probe_role = pair.get(f"{probe_url}/role", "GET /role", failures)
            duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
            standby_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            islanded = {
                "probe": probe_role.get("role"),
                "probe_sync": stranded_rejoin.sync_kind(probe_role),
                "duty": duty_role.get("role"),
                "duty_sync": stranded_rejoin.sync_kind(duty_role),
                "standby": standby_role.get("role"),
                "standby_sync": stranded_rejoin.sync_kind(standby_role),
            }
            if orphan_report(probe_role):
                break
        evidence["island"] = islanded
        if failures:
            raise Abort
        if not orphan_report(pair.get(f"{probe_url}/role", "GET /role",
                                      failures)):
            raise Inconclusive(
                "the pinned probe never reported the orphaned verdict "
                "on the demoted owner — the launched tooling predates "
                "the ownerless-source apply the orphan episode is "
                "staged on",
                f"the island readings were {islanded}",
            )
        first = episode_counts(
            pair.get(f"{probe_url}/journal", "GET /journal", failures),
            probe_path,
            floors,
        )
        evidence["first_entry"] = first
        if first["served"] != 1 or first["durable"] != 1:
            raise Inconclusive(
                "the pinned probe's orphaned transition left "
                f"{first} field_orphaned records instead of exactly one "
                "on each surface — the pinned release predates the "
                "journaled orphan contract the episode's bound is "
                "counted on"
            )
        digest_entries.append(
            {
                "phase": "island",
                "probe": islanded["probe"],
                "probe_sync": islanded["probe_sync"],
                "duty": islanded["duty"],
                "duty_sync": islanded["duty_sync"],
                "standby": islanded["standby"],
                "standby_sync": islanded["standby_sync"],
            }
        )

        # Phase 4 — the held window: the tracking source frozen past
        # the pull timeout and thawed again, so produced-nothing misses
        # interleave with completed ownerless pulls across the episode.
        window, budget_closed = watch_round(
            duty_url, probe_url, rig.duty, failures, evidence, HOLD_ROUNDS
        )
        digest_entries.append(
            {"phase": "window", "window": window,
             "budget": "closed" if budget_closed else "open"}
        )
        if failures:
            raise Abort

        # The doctored case — the leg asserting a journaled entry per
        # ownerless pull, the flood the contract closed. The honest
        # single entry must fail it.
        counts = episode_counts(
            pair.get(f"{probe_url}/journal", "GET /journal", failures),
            probe_path,
            floors,
        )
        evidence["episode_one"] = counts
        if tamper == "expect-flood":
            failures.append(
                f"{TAMPER_EVIDENCE} — the honest window journaled "
                f"{counts['served']} served and {counts['durable']} "
                "durable field_orphaned records for the episode, not "
                "one per ownerless pull"
            )
            raise Abort

        # The verdict clause: every observation the probe served while
        # it held the pinned posture reads `orphaned`. The produced-
        # nothing misses must not flicker it to `degraded` — the
        # transition they would re-journal on every later apply.
        standing = [row for row in window if row["role"] == "standby"]
        flicked = next(
            (row for row in standing if row["sync"] != "orphaned"), None
        )
        if flicked is not None:
            failures.append(
                "the pinned probe reported "
                f"{flicked['sync']} inside the held episode — the "
                "standing orphaned verdict must ride out the "
                "evidence-free pull misses: "
                f"{json.dumps(window)[:300]}"
            )
            raise Abort
        if len(standing) < 2:
            raise Inconclusive(
                f"the held window collected {len(standing)} pinned "
                "observation(s) — the episode was too short to show the "
                "verdict riding out an evidence-free pull miss: "
                f"{json.dumps(window)[:300]}"
            )
        # The miss-accounting clause: the produced-nothing misses and
        # the ownerless applies behind them both count toward the armed
        # budget, and the count never rewinds.
        misses = [
            row["failover"][1]
            for row in standing
            if len(row["failover"]) == 3
        ]
        evidence["misses"] = misses
        if len(misses) < 2:
            raise Inconclusive(
                "the pinned probe served no armed miss accounting "
                "across the held window — the pinned release predates "
                "the heartbeat evidence the episode's budget clause "
                f"reads: {json.dumps(window)[:300]}"
            )
        if any(not isinstance(count, int) for count in misses) or any(
            cur < prev for prev, cur in zip(misses, misses[1:])
        ):
            failures.append(
                "the armed miss accounting did not advance "
                f"monotonically across the held window: {misses} — the "
                "evidence-free pull misses must still count toward the "
                "armed budget"
            )
            raise Abort
        if misses[-1] <= misses[0]:
            raise Inconclusive(
                "the held window advanced the armed miss accounting by "
                f"{misses[-1] - misses[0]} pull(s) — the alternation "
                "never staged the produced-nothing misses the "
                "episode's budget clause is read across: "
                f"{json.dumps(window)[:300]}"
            )
        if counts["served"] != 1 or counts["durable"] != 1:
            failures.append(
                "the orphan episode journaled "
                f"{counts['served']} served and {counts['durable']} "
                "durable field_orphaned records instead of exactly one "
                "on each surface — the bound is per-episode, never per "
                f"ownerless pull: {json.dumps(window)[:300]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "episode",
                "served": counts["served"],
                "durable": counts["durable"],
                "misses": misses,
                "verdict": "held",
                "budget": "closed" if budget_closed else "open",
            }
        )

        # Phase 5 — the second episode: the field comes back to the
        # launch owner, whose non-orphaned apply closes the episode,
        # and a second demotion islands the pair again — a genuinely
        # new orphan episode, which owes its own entry. The bound is
        # per-episode, not per-process.
        ended = None
        for _ in range(EPISODE_TWO_SCANS):
            if promoted(duty_url, failures, "the demoted field owner") is None:
                break
            pair.scan(probe_url, failures)
            ended = pair.get(f"{probe_url}/role", "GET /role", failures)
            if claim_reclaim.tracking(ended):
                break
            pair.scan(duty_url, failures)
        evidence["ended"] = stranded_rejoin.sync_kind(ended)
        if failures:
            raise Abort
        if not claim_reclaim.tracking(ended):
            raise Inconclusive(
                "the field claim never came back to the launch owner, "
                "so the probe's next applies stayed ownerless — the "
                "episode's non-orphaned close never landed: "
                f"{json.dumps(ended)[:200]}"
            )
        second_status, second_report = pair.request(f"{duty_url}/demote", {})
        evidence["second_demote"] = {
            "status": second_status,
            "body": second_report,
        }
        if second_status != 200:
            failures.append(
                "the episode-reopening POST /demote answered "
                f"{second_status} {second_report} — the second "
                "episode never staged"
            )
            raise Abort
        reorphaned = None
        for _ in range(EPISODE_TWO_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            pair.scan(probe_url, failures)
            reorphaned = pair.get(f"{probe_url}/role", "GET /role", failures)
            if orphan_report(reorphaned):
                break
        evidence["reorphaned"] = stranded_rejoin.sync_kind(reorphaned)
        if not orphan_report(reorphaned):
            failures.append(
                "the second demotion left the pinned probe on "
                f"{stranded_rejoin.sync_kind(reorphaned)} — a genuinely "
                "new orphan episode never began: "
                f"{second_status} {second_report}"
            )
            raise Abort
        second = episode_counts(
            pair.get(f"{probe_url}/journal", "GET /journal", failures),
            probe_path,
            floors,
        )
        evidence["episode_two"] = second
        if second["served"] != 2 or second["durable"] != 2:
            failures.append(
                "the second orphan episode journaled "
                f"{second['served']} served and {second['durable']} "
                "durable field_orphaned records instead of exactly two "
                "on each surface — a genuinely new episode owes its own "
                "entry"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "episode-two",
                "closed": evidence["ended"],
                "served": second["served"],
                "durable": second["durable"],
            }
        )

        # Phase 6 — the launch layout restored: the field claimed back
        # by the manifest's duty member with the declared standby
        # `tracking` it, read through both served role reports after a
        # settle train in the documented tracking-first order.
        if promoted(duty_url, failures, "the launch owner") is None:
            raise Abort
        handover = []
        for _ in range(SETTLE_SCANS):
            _tracked, owner_snapshot = rig.tick(
                standby_url, duty_url, failures,
                diverged="the restored pair's images diverged at tick "
                "{tick} — the orphan episode's restore was not "
                "bumpless",
            )
            handover.append(owner_snapshot["tick"])
        pair.scan(probe_url, failures)
        final_duty = pair.get(f"{duty_url}/role", "GET /role", failures)
        final_standby = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        final_probe = pair.get(f"{probe_url}/role", "GET /role", failures)
        evidence["restored_tick"] = final_duty.get("tick")
        if final_duty.get("role") != "active" or not claim_reclaim.tracking(
            final_standby
        ):
            failures.append(
                "the pair did not restore its launch roles — the field "
                "owner and the declared tracking standby must hold the "
                f"field between them: {json.dumps(final_duty)[:200]} / "
                f"{json.dumps(final_standby)[:200]}"
            )
            raise Abort
        if not claim_reclaim.tracking(final_probe):
            failures.append(
                "the pinned probe did not settle a tracking standby on "
                f"the restored owner: {json.dumps(final_probe)[:200]}"
            )
            raise Abort
        if not driver_recovery.roles_hold(
            rig, failures, "after the orphan episode restored"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": handover,
                "duty": final_duty.get("role"),
                "standby": "tracking",
                "probe": "tracking",
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(probe)
        if rig is not None:
            thaw(rig.duty)
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
        choices=["expect-flood"],
        help="doctor the leg's expectation to the defect shape — a "
        "field_orphaned entry per ownerless pull — so the pass must "
        "fail naming the single entry the honest journal carries",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            orphan_episode_bound_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "orphan-episode-bound: "
                f"{TAMPER_EVIDENCE} — an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"orphan-episode-bound: inconclusive — {detail}")
        print(f"orphan-episode-bound-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"orphan-episode-bound: {line}")
        return 1
    for failure in failures:
        eprint(f"orphan-episode-bound: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"orphan-episode-bound: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"orphan-episode-bound-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the demoted pair's island pinned the "
        "pinned probe orphaned with "
        f"{evidence['episode_one']['served']} served and "
        f"{evidence['episode_one']['durable']} durable "
        "field_orphaned record for the episode while the "
        "produced-nothing misses counted "
        f"{evidence['misses'][0]} → {evidence['misses'][-1]} toward the "
        "armed budget, the second episode owed its own entry "
        f"({evidence['episode_two']['served']} served), and the launch "
        "roles stand with the claim back on the duty member at tick "
        f"{evidence['restored_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())