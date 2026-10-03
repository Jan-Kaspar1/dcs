#!/usr/bin/env python3
"""The standby-dns-resume leg for the reference plant — the
consumer-side proof that the #1081 deferred-resolution contract holds
on the manifest-declared pair (WW-ENG-003, WW-LCM-001 — mirrored at
the customer boundary from the qa rig's `standby-dns-resume`
scenario): a field-owning controller relaunched onto its declared
`--state-file` with its tracking-source name doctored to an
unresolvable value stays up owning the field — the standby
resolution failure is the named degraded standby condition its demote
later reports, never the fatal boot error a pre-contract startup
produced.

The standby-restart leg (`ci/legs/standby_restart.py`) proves a
tracking peer's declared-files resume and reconvergence, and the
orchestrated-restart leg (`ci/legs/orchestrated_restart.py`) proves
the health-sequenced roll — both relaunch peers with their configured
flags, so no leg staged a name-resolution failure at resume until
this one. The harness's flag-doctoring lever is the consumer
boundary's counterpart of the qa runner's `relaunch_controller` — a
rebuilt spawn argv where the rig recreates the container's launch
command. The run:

- converges the manifest-declared pair to `tracking` through the pair
  rig's driven-tick loop and gates the run on the duty member's
  declared `--state-file` — the resume the leg stages;
- relaunches the field owner with `--peer` naming
  `dcs-peer-down.invalid:8080` — the flag through which an owner
  names the peer it tracks if demoted, the "--standby name" an owning
  run carries — an RFC-2606 `.invalid` name no resolver answers, so
  every pull the demotion runs on it counts a resolution miss. The
  respawn rebinds the member's own monitor address the way a
  container restart keeps its published port, so the tracking
  sibling's configured wiring holds;
- asserts the resumed process stays up owning the field: `GET /role`
  reporting `active` at the persisted tick, driven scans advancing,
  the served surface answering (`GET /signals`, `GET /snapshot`), a
  receipted `write_value` settling `applied` and landing on the
  served point — and the tracking sibling never taking the field
  inside the relaunch window;
- demotes the resumed owner — a configured `--peer` satisfying the
  demotion guard — onto the doctored source: each driven pull
  re-resolves the name and misses, the still-serving monitor's
  `sync.degraded` carrying the named resolution failure in place of
  the observed exit(1);
- restores the pair's launch flags and roles: a plain relaunch on the
  declared files takes the field the demote released through the
  conditional startup grant, and the sibling's tracking-first pulls
  reconverge — the pair's launch roles restored for the legs behind
  this one.

The contract postdates the pinned v0.3.0 release: a relaunch whose
unresolvable tracking source fails startup — the pre-contract
`error: cannot resolve` exit — or a pinned tooling carrying no
`--peer` flag at all reports `standby-dns-resume-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract.

Usage:

    standby_dns_resume.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `standby-dns-resume-digest <sha256>` line prints —
the check runs two passes and compares them
(`standby-dns-resume-nondeterministic`). A contract violation reports
`standby-dns-resume: …` lines on stderr and exits 1 — the check's
`standby-dns-resume-failed`. `--tamper expect-exit` doctors the leg's
own expectation to the defect shape — asserting the doctored relaunch
exited — so the leg proves its stays-up assertion fires on the honest
run rather than passing an unexercised contract.
"""

import argparse
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import force_carryover
import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the resumed owner exited on the
# unresolvable tracking name — the pre-contract shape the contract
# closed — must surface the named diagnostic on the honest stays-up
# run rather than passing an unexercised contract.
LEG = {
    "order": 460,
    "title": "the standby-dns-resume leg",
    "passes": "standby-dns-resume-leg",
    "tampers": [
        {
            "name": "expect-exit",
            "passed": "an expect-exit case passed the standby-dns-resume leg",
            "missed": "the expect-exit case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the resumed owner exiting"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the harness admits no lever
    for — the contract the leg exercises: the deferred tracking-source
    resolution, the `--peer` flag an owning run's tracking name rides,
    or the declared state file the resume reads. The run classifies
    inconclusive, never a product failure."""


# The doctored tracking source: an RFC-2606 `.invalid` name no
# resolver answers — the same unresolvable shape #1081's controller
# tests and the qa rig's relaunch lever bind — so every checkpoint
# pull the demoted peer runs on it counts a resolution miss.
UNRESOLVABLE_TRACK = "dcs-peer-down.invalid:8080"

# The driven-scan bounds each phase gets: the owning assertions' fixed
# advance window, the receipted write's apply window, the demote's
# demoting->standby walk and the degraded-verdict wait, and the
# restore's tracking-first reconvergence. The actor and reason the
# leg's receipted write declares.
OWNING_SCANS = 3
APPLY_SCANS = 4
SETTLE_SCANS = 6
RECONVERGE_SCANS = 8
ACTOR = "ci-standby-dns-resume"
REASON = "standby-dns-resume"


def respawn_active(args, rig, files, listen, peer=None):
    """Relaunch the field-owning member on its declared persistence —
    the pair harness's spawn vocabulary carried by hand so the
    tracking-source flag can carry the doctored name: `peer` becomes
    `--peer`, the argument through which a field owner names the peer
    it tracks if demoted — the "--standby name" an owning run
    carries; `peer=None` recreates the launch flags unchanged, the
    restore half a flag rewrite needs since no start un-applies one.
    Returns `(process, url, preamble)` — `url` None when the process
    exits before reporting a listener, the preamble then carrying the
    startup refusal's stderr lines."""
    argv = [
        args.controller,
        args.model,
        "--remote",
        rig.plant_addr,
        "--driven",
        "--listen",
        listen,
        "--dt",
        str(args.dt),
    ]
    if peer is not None:
        argv += ["--peer", peer]
    argv += ["--pair-token", pair.PAIR_TOKEN]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
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


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def degraded_verdict(report):
    """The demoted resumed peer's degraded-standby verdict: the served
    `sync.degraded` must name the doctored tracking source's
    resolution failure — the named degraded standby evidence the
    contract puts in place of the observed exit(1). Returns `named`,
    `foreign` (a degraded record that never names the doctored
    resolution), or None while no degraded detail serves."""
    detail = ((report.get("sync") or {}).get("degraded") or {}).get(
        "detail"
    )
    if not isinstance(detail, str):
        return None
    if UNRESOLVABLE_TRACK in detail and "resolve" in detail:
        return "named"
    return "foreign"


def dns_resume_pass(args, tamper):
    """The standby-dns-resume run: converge, relaunch the field owner
    on a doctored unresolvable tracking name, assert the resumed
    process stays up owning the field, demote it onto the name for
    the named degraded evidence, restore the launch flags and roles.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract or
    the harness admits no lever."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "standby-dns-resume leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the leg's receipted "
            "write has nothing to land on"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_listen = duty_url.removeprefix("http://")
        duty_files = rig.duty_files
        if duty_files.get("state_file") is None:
            raise Inconclusive(
                "the manifest's duty controller declares no "
                "state_file — the resume the leg stages has nothing "
                "to resume from"
            )

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint — the settled launch layout the leg's
        # restore owes, and the persisted run the relaunch resumes.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        baseline = simulate.snapshot_point(owner, point)
        if baseline is None or "bool" not in baseline:
            raise Abort(
                f"the write target point {point} serves {baseline} — "
                "a bool baseline the leg can flip is required"
            )
        written = {"bool": not baseline["bool"]}

        # Phase 2 — the doctored relaunch: the field owner's process
        # stops and respawns on its own monitor address and declared
        # files with `--peer` naming the unresolvable standby — the
        # flag-doctoring lever the qa rig's container recreate runs.
        # A pre-contract release exits on the resolution failure
        # instead of serving — the pinned release predating the
        # contract — and a tooling carrying no `--peer` admits no
        # lever at all; both classify inconclusive.
        pair.stop(rig.duty)
        rig.duty, url, preamble = respawn_active(
            args, rig, duty_files, duty_listen, peer=UNRESOLVABLE_TRACK
        )
        rig.duty_url = url
        evidence["relaunch_preamble"] = preamble
        if url is None:
            joined = " ".join(preamble)
            if "unknown option" in joined:
                raise Inconclusive(
                    "the pinned tooling carries no --peer flag — the "
                    "harness admits no flag-doctoring lever for the "
                    "field owner's tracking source"
                )
            if "cannot resolve" in joined:
                raise Inconclusive(
                    "the unresolvable tracking source failed the "
                    "resumed owner's startup — the pinned release "
                    "predates the deferred-resolution contract"
                )
            failures.append(
                "the relaunched owner exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        duty_url = rig.duty_url
        if tamper == "expect-exit":
            # The doctored expectation — the leg asserts the doctored
            # relaunch exited on the unresolvable name, the observed
            # exit(1) the contract closed. The honest stays-up run
            # must fail it.
            failures.append(
                "the doctored expectation wanted the resumed owner "
                "exiting on its unresolvable tracking source — the "
                "honest run stayed up owning the field"
            )
            raise Abort
        line = next(
            (line for line in preamble if "resumed from state file" in line),
            None,
        )
        if line is None:
            failures.append(
                "the relaunched owner never reported a resume — its "
                "cold start silently abandons the persisted run at "
                f"tick {converged['ticks'][-1]}"
            )
            raise Abort
        match = re.search(r"at tick (\d+)", line)
        resumed_tick = int(match.group(1)) if match else None
        evidence["resumed_tick"] = resumed_tick
        if resumed_tick != converged["ticks"][-1]:
            failures.append(
                f"the relaunch resumed at tick {resumed_tick}, the "
                f"converged run persisted {converged['ticks'][-1]}"
            )
            raise Abort

        # Phase 3 — the owning assertions: the resumed process claims
        # the released field through the conditional startup grant and
        # keeps serving — its /role answering active, scans advancing,
        # the served surface answering, a receipted write landing —
        # while the tracking sibling never takes the field inside the
        # relaunch window.
        report = pair.get(f"{duty_url}/role", "GET /role", failures)
        if report.get("role") != "active":
            failures.append(
                f"the resumed owner reports {report.get('role')!r} — "
                "the unresolvable standby name kept it from the field "
                "it resumed as owner of"
            )
            raise Abort
        sibling = pair.get(f"{standby_url}/role", "GET /role", failures)
        if sibling.get("role") == "active":
            failures.append(
                "the tracking sibling took the field inside the "
                "relaunch window — the resumed process's refused "
                "grant ended it before the contract could run"
            )
            raise Abort
        tick0 = report.get("tick") or 0
        snapshot = None
        for _ in range(OWNING_SCANS):
            snapshot = pair.scan(duty_url, failures)
        evidence["advanced_tick"] = snapshot["tick"]
        if snapshot["tick"] <= tick0:
            failures.append(
                f"the resumed owner's tick never advanced past "
                f"{tick0} — the process serves but does not scan"
            )
            raise Abort
        pair.get(f"{duty_url}/signals", "GET /signals", failures)
        command = takeover.write_value(point, written["bool"])
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": command, "actor": ACTOR, "reason": REASON},
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the resumed owner's command path refused the "
                f"receipted write: {status} {receipt}"
            )
            raise Abort
        landed = None
        for _ in range(APPLY_SCANS):
            snapshot = pair.scan(duty_url, failures)
            receipts = pair.get(
                f"{duty_url}/receipts", "GET /receipts", failures
            )
            if takeover.settled(receipts, command):
                landed = snapshot
                break
        if landed is None:
            failures.append(
                "the resumed owner's receipted write never settled "
                "applied — the field stayed unwritable through the "
                "owner the relaunch resumed"
            )
            raise Abort
        served = simulate.snapshot_point(landed, point)
        if served != written:
            failures.append(
                f"the resumed owner's write never reached the served "
                f"point — {served} at point {point}, expected {written}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "resume-owning",
                "resumed_tick": resumed_tick,
                "scans": "advanced",
                "served": "answered",
                "write": "applied",
            }
        )

        # Phase 4 — the named degraded half: demoting the resumed
        # owner lands it on its doctored tracking source — the
        # configured `--peer` satisfying the demotion guard — where
        # every checkpoint pull re-resolves the name and misses. The
        # standby resolution failure must read as the named
        # `sync.degraded` evidence on the still-serving monitor, never
        # a process exit.
        rig.demote(duty_url, failures, "the resumed owner")
        demoted = None
        for _ in range(SETTLE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            if report.get("role") == "standby":
                demoted = report
                break
        if demoted is None:
            failures.append(
                "the resumed owner never landed the standby role "
                f"after its demote — GET /role answers {report}"
            )
            raise Abort
        verdict = None
        for _ in range(SETTLE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            evidence["degraded_report"] = report
            verdict = degraded_verdict(report)
            if verdict is not None:
                break
        if verdict != "named":
            if verdict == "foreign":
                failures.append(
                    "the demoted resumed peer's standby evidence "
                    "never named the doctored tracking source's "
                    "resolution failure — the contract's named "
                    f"degraded condition — served {report.get('sync')}"
                )
            else:
                failures.append(
                    "the demoted resumed peer never reported the "
                    "named degraded standby condition — GET /role "
                    f"answers {report.get('sync')}"
                )
            raise Abort
        digest_entries.append(
            {
                "phase": "demoted",
                "role": "standby",
                "degraded": "named",
            }
        )

        # Phase 5 — the restore half: relaunch the launch flags back —
        # the demote released the field's claim, so the recreated
        # active's conditional startup grant takes it — and drive the
        # tracking-first pair ticks until the sibling's pulls
        # reconverge behind the restored owner.
        pair.stop(rig.duty)
        rig.duty, url, preamble = respawn_active(
            args, rig, duty_files, duty_listen
        )
        rig.duty_url = url
        evidence["restore_preamble"] = preamble
        if url is None:
            failures.append(
                "the restore relaunch exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        duty_url = rig.duty_url
        report = pair.get(f"{duty_url}/role", "GET /role", failures)
        if report.get("role") != "active":
            failures.append(
                f"the restored owner reports {report.get('role')!r} — "
                "the conditional startup grant never retook the field "
                "the demote released"
            )
            raise Abort
        settled = None
        for _ in range(RECONVERGE_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            sibling = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            owner = pair.get(f"{duty_url}/role", "GET /role", failures)
            if owner.get("role") == "active" and tracking(sibling):
                settled = (owner, sibling)
                break
        if settled is None:
            failures.append(
                "the pair never settled back to the launch roles — "
                f"{duty_decl['name']} owning with "
                f"{standby_decl['name']} tracking: owner "
                f"{owner.get('role')!r}, sibling {sibling.get('sync')}"
            )
            raise Abort
        evidence["restored_tick"] = settled[0].get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "duty_role": "active",
                "standby_sync": "tracking",
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
        choices=["expect-exit"],
        help="doctor the leg's expectation to the defect shape — the "
        "pass must fail naming the stays-up record the honest run "
        "left",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = dns_resume_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "standby-dns-resume: the doctored expectation wanted "
                "the resumed owner exiting on its unresolvable "
                "tracking source — an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        eprint(f"standby-dns-resume: inconclusive — {inconclusive}")
        print(
            f"standby-dns-resume-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"standby-dns-resume: {line}")
        return 1
    for failure in failures:
        eprint(f"standby-dns-resume: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"standby-dns-resume: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"standby-dns-resume-digest {digest} — the field owner "
        f"resumed at tick {evidence['resumed_tick']} on the doctored "
        f"{UNRESOLVABLE_TRACK} tracking source, stayed up owning the "
        "field, demoted onto the name as the named degraded standby "
        "evidence, and restored the launch roles and flags at tick "
        f"{evidence['restored_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
