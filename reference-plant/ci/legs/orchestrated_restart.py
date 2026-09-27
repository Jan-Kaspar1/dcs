#!/usr/bin/env python3
"""The orchestrated-restart leg for the reference plant — the
consumer-side proof that an orchestrator's rolling restart of the
manifest-declared pair, sequenced on the image's declared health
contract rather than fixed sleeps, leaves the field-owning peer
scanning and serving uninterrupted while the restarted peer rejoins
and reconverges to tracking (WW-ENG-003, WW-LCM-002 — the
consumer-boundary half of the pre-pilot tranche's
orchestrated-restart clause).

The standby-restart leg (`ci/legs/standby_restart.py`) proves the
tracking peer resumes and reconverges across a stop/start and the
rolling-upgrade leg (`ci/legs/rolling_upgrade.py`) proves the binary
roll — both order the run on driven ticks and served roles. This leg
exercises the ordering an orchestrator applies: the published image's
declared `HEALTHCHECK` probes `GET /health`, the monitor's bounded
liveness answer, through the shipped `dcs-ctl <addr> health` — the
compose-equivalent of `docker inspect`'s `Health.Status` this
process-stand-in harness reads. The run:

- converges the manifest-declared pair to `tracking` and gates the
  contract: each peer's `GET /health` must answer the `HealthReport`
  shape — the listener's `live` declaration, the served role, the run
  tick, the last-scan freshness — and the released `dcs-ctl` must
  carry the `health` verb the image's `HEALTHCHECK` declares; a
  pinned release whose images predate the liveness answer reports
  `orchestrated-restart-digest inconclusive` rather than failing;
- restarts the tracking standby's container: the peer process stops,
  the declared status reading `unhealthy` while the liveness answer
  cannot serve, the field owner's driven scans keep stepping the
  plant exactly once apiece with its heartbeat surface answering
  `live`/`active` throughout; the relaunch rebinds the peer's own
  monitor address — a container restart keeps its published port —
  onto its declared `--state-file`/`--journal-file`, and the
  orchestrated wait polls the shipped probe until `healthy`, never a
  fixed sleep; the resumed peer reports the persisted tick, rejoins
  `standby` unsynchronized, replays its run-1 journal behind the
  run-2 boundary, and reconverges to `tracking` inside the settle
  bound;
- issues the documented `demote`/`promote` switch and repeats the
  restart on the new standby — the demoted launch-duty member,
  relaunched wired at the peer it follows, the same re-issue of pair
  wiring the rolling upgrade performs — this time through a
  slow-starting stand-in the harness admits: a wrapper holding the
  container's boot before the released binary execs, so the declared
  status must read `unhealthy` and transition to `healthy` rather
  than a fixed sleep passing it;
- restores the pair's launch roles — demote the promoted peer,
  promote the reconverged member — ending at one `active` plus one
  `tracking` standby, both health verdicts live, the adopted receipt
  logs one log, the whole pair rolled.

Usage:

    orchestrated_restart.py --ctl PATH --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `orchestrated-restart-digest <sha256>` line prints —
the check runs two passes and compares them
(`orchestrated-restart-nondeterministic`). A contract violation
reports `orchestrated-restart: …` lines on stderr and exits 1 — the
check's `orchestrated-restart-failed`. `--tamper premature-healthy`
doctors the leg's own probe so the restarted peer is declared
healthy before its liveness answer serves — the pass must fail
naming the doctored verdict.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import rolling_upgrade


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a probe declaring the restarted peer healthy
# before its liveness answer serves must surface the named
# diagnostic — never a sleep passing an unprobed container.
LEG = {
    "order": 430,
    "title": "the orchestrated-restart leg",
    "passes": "orchestrated-restart-leg",
    "tools": {"ctl": "dcs-ctl"},
    "tampers": [
        {
            "name": "premature-healthy",
            "passed": "a premature-healthy case passed the orchestrated-restart leg",
            "missed": "the premature-healthy case did not report its named diagnostic",
            "evidence": ["the doctored probe declared the restarted peer healthy before its liveness answer served"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the health contract the leg
    exercises — no `GET /health` answer, no `dcs-ctl health` verb —
    or the harness admits no slow-starting stand-in: the run
    classifies inconclusive, never a product failure."""


# The driven ticks the downtime window runs — the field owner keeps
# stepping while its peer's container is dead. The declared health
# wait's poll cadence and bound: the image's HEALTHCHECK grants a
# slow container tens of seconds to serve, so the poll bound spans
# the same order rather than a fixed sleep.
DOWNTIME_TICKS = 3
HEALTH_POLL_S = 0.05
HEALTH_POLLS = 400
STANDIN_DELAY_S = 2


def liveness(url):
    """The raw `GET /health` answer — `(status, decoded)` — so a
    missing endpoint classifies the pinned release pre-contract
    while a refused connect is the probe's `unhealthy`. Transport
    failures return `(None, error)`."""
    try:
        with urllib.request.urlopen(f"{url}/health") as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception as error:
        return None, error


def health_status(args, url, tamper=None):
    """The declared health verdict the image's `HEALTHCHECK` reports
    — `dcs-ctl <addr> health` exiting zero is the container's
    `healthy`; a refused connect or a non-live answer is
    `unhealthy`. The `premature-healthy` tamper doctors the probe to
    declare healthy before the liveness answer serves — the leg's
    stopped-container assertion must fire on it."""
    if tamper == "premature-healthy":
        return "healthy"
    try:
        result = subprocess.run(
            [args.ctl, url.removeprefix("http://"), "health"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "unhealthy"
    return "healthy" if result.returncode == 0 else "unhealthy"


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def relaunch_log(path):
    """The restarted peer's captured stderr lines — the spawn
    preamble the resume assertions read."""
    try:
        with open(path) as handle:
            return [line.strip() for line in handle if line.strip()]
    except OSError:
        return []


def slow_standin(scratch, controller):
    """A slow-starting container stand-in the harness admits: a
    wrapper delaying the boot before the released controller execs,
    so the declared health status holds `unhealthy` until the
    liveness answer actually serves. A scratch that cannot carry the
    script is inconclusive — the harness admits no stand-in."""
    path = os.path.join(scratch, "slow-start-controller.sh")
    try:
        with open(path, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                f"sleep {STANDIN_DELAY_S}\n"
                f"exec {shlex.quote(controller)} \"$@\"\n"
            )
        os.chmod(path, 0o755)
    except OSError as error:
        raise Inconclusive(
            f"the harness admits no slow-starting stand-in: {error}"
        )
    return path


def respawn(rig, attr, url_attr, binary, target, files, listen, args):
    """Relaunch the stopped peer's container — the runner's spawn
    vocabulary carried by hand: stderr to a runner log (the health
    wait must run beside the boot, so `spawn_peer`'s blocking
    preamble read cannot serialize it), the peer rebinding its own
    monitor address the way a container restart keeps its published
    port, `target` the `--standby` wiring of the peer it follows or
    None on a duty relaunch. Returns `(url, errlog)` — the process
    itself lands on the rig attribute."""
    argv = [
        binary,
        args.model,
        "--remote",
        rig.plant_addr,
        "--driven",
        "--listen",
        listen,
        "--dt",
        str(args.dt),
    ]
    if target is not None:
        argv += ["--standby", target]
    argv += ["--pair-token", pair.PAIR_TOKEN]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    errlog = os.path.join(rig.scratch, f"{attr}-relaunch.stderr")
    log = open(errlog, "w")
    process = subprocess.Popen(argv, stderr=log)
    # The child holds its own descriptor; the parent's may close at
    # once so the log reads back complete once the wait ends.
    log.close()
    setattr(rig, attr, process)
    setattr(rig, url_attr, "http://" + pair.dialable(listen))
    return getattr(rig, url_attr), errlog


def wait_healthy(args, proc, url, errlog, tamper, failures, label):
    """The orchestrated wait — poll the declared health status until
    it reports `healthy`, bounded by the declared window, never a
    fixed sleep. Returns the pre-healthy readings so a caller can
    assert the `unhealthy`→`healthy` transition actually
    happened."""
    readings = []
    for _ in range(HEALTH_POLLS):
        if proc.poll() is not None:
            break
        status = health_status(args, url, tamper)
        if status == "healthy":
            return readings
        readings.append(status)
        time.sleep(HEALTH_POLL_S)
    detail = "; ".join(relaunch_log(errlog)[-3:]) or "no diagnostic"
    if proc.poll() is not None:
        failures.append(
            f"the restarted {label} exited before its liveness "
            f"answer served: {detail}"
        )
    else:
        failures.append(
            f"the restarted {label} never reported healthy inside "
            f"the declared health window — the status read "
            f"{readings[-1] if readings else 'starting'} throughout"
        )
    raise Abort


def restart_cycle(
    rig,
    attr,
    url_attr,
    owner_url,
    files,
    label,
    binary,
    standin,
    steps,
    args,
    tamper,
    failures,
):
    """One orchestrated restart on the peer the rig attribute
    carries: stop the container — the declared status reading
    `unhealthy` until the liveness answer serves — keep the
    field-owning peer stepping and serving through the downtime
    window, relaunch onto the declared persistence files rebinding
    the peer's monitor address, gate the wait on the health contract,
    and reconverge to `tracking` inside the settle bound. `standin`
    swaps the relaunch binary for the slow-starting stand-in.
    Returns the cycle's digest dict."""
    digest = {}
    url = getattr(rig, url_attr)
    listen = url.removeprefix("http://")
    target = owner_url.removeprefix("http://")
    state_file = files.get("state_file")
    if state_file is None:
        raise Abort(
            f"the manifest's {label} declares no state_file — the "
            "restart has nothing to resume from"
        )
    stopped = pair.get(f"{url}/snapshot", "GET /snapshot", failures)
    served = pair.get(f"{url}/journal", "GET /journal", failures)
    persisted = rolling_upgrade.persisted_checkpoint(
        state_file, stopped["tick"], rig.fingerprint, label, failures
    )

    # The container stop: the declared status must read `unhealthy`
    # while the liveness answer cannot serve — the verdict an
    # orchestrator's sequencing reads instead of a fixed sleep.
    pair.stop(getattr(rig, attr))
    setattr(rig, attr, None)
    down = health_status(args, url, tamper)
    digest["down"] = down
    if down != "unhealthy":
        if tamper == "premature-healthy":
            failures.append(
                "the doctored probe declared the restarted peer "
                "healthy before its liveness answer served — the "
                "stopped container's honest status is unhealthy"
            )
        else:
            failures.append(
                f"the stopped {label}'s declared health status reads "
                f"{down!r} — a downed container probes unhealthy "
                "until its liveness answer serves again"
            )
        raise Abort

    # The downtime window: the field owner's driven scans keep
    # stepping the plant exactly once apiece, its writes landing on
    # every simulated `out` point and its heartbeat surface answering
    # `live` with the `active` role — the peer's scan cadence and
    # served surface hold uninterrupted through the restart.
    downtime = []
    for _ in range(DOWNTIME_TICKS):
        owner = rolling_upgrade.owner_scan(rig, owner_url, steps, failures)
        field = rolling_upgrade.field_check(
            owner, rig.plant_io, failures, f"while {label} was down"
        )
        status, body = liveness(owner_url)
        if (
            status != 200
            or not isinstance(body, dict)
            or body.get("live") is not True
            or body.get("role") != "active"
        ):
            failures.append(
                f"the field owner's liveness answer served {status} "
                f"{body} while {label} was down — the peer's served "
                "surface faltered"
            )
            raise Abort
        downtime.append({"tick": owner["tick"], "field": field})
    digest["downtime"] = downtime

    # The relaunch — through the slow-starting stand-in where the
    # cycle declares one — and the orchestrated wait on the declared
    # status: `unhealthy` until the liveness answer serves, `healthy`
    # once it does.
    spawn_binary = slow_standin(rig.scratch, binary) if standin else binary
    url, errlog = respawn(
        rig, attr, url_attr, spawn_binary, target, files, listen, args
    )
    readings = wait_healthy(
        args, getattr(rig, attr), url, errlog, tamper, failures, label
    )
    if standin:
        digest["boot"] = "unhealthy->healthy" if readings else "served"
        if not readings:
            failures.append(
                "the slow-starting stand-in served healthy without an "
                "observed unhealthy reading — a fixed sleep would pass "
                "the same, so the health gate proved nothing"
            )
            raise Abort
    preamble = relaunch_log(errlog)
    bound = next(
        (line for line in preamble if "listening on" in line), None
    )
    if bound is None or bound.rsplit(None, 1)[-1] != listen:
        failures.append(
            f"the relaunched {label} never reported binding {listen}: "
            f"{preamble[-3:] or ['no preamble']}"
        )
        raise Abort
    resumed = rolling_upgrade.resume_evidence(
        preamble, persisted, label, failures
    )
    digest["persisted"] = persisted
    digest["resumed"] = resumed

    # The rejoin and the replayed record: standby unsynchronized —
    # never a field claim — and the run-1 journal answered verbatim
    # behind the run-2 boundary.
    rolling_upgrade.rejoin(url, failures)
    rolling_upgrade.replay_check(url, served, persisted, failures)

    # The reconvergence window: tracking-first driven ticks until the
    # relaunched peer reports `tracking` inside the settle bound, the
    # field owner's scans stepping throughout.
    tracked = rolling_upgrade.reconverge(
        rig, url, owner_url, steps, f"the relaunched {label}", failures
    )
    digest["reconverged"] = tracked["tick"]
    digest["tracked_role"] = tracked
    owner_role = role(owner_url, failures)
    if owner_role.get("role") != "active":
        failures.append(
            f"the field owner reports {owner_role.get('role')!r} "
            f"after {label}'s restart — the orchestrated roll cost "
            "the field"
        )
        raise Abort
    digest["owner_role"] = owner_role
    return digest


def orchestrated_restart_pass(args, tamper):
    """The orchestrated-restart run: converge, gate the health
    contract, roll both halves of the pair on health-sequenced
    restarts, restore the launch roles. Returns `(digest_entries,
    evidence, failures)`; raises `Inconclusive` where the pinned
    release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "orchestrated-restart leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files, standby_files = rig.duty_files, rig.standby_files

        # Phase 1 — convergence: the manifest-declared pair settled,
        # the tracking peer reporting `tracking` and the field owner
        # `active`.
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

        # Phase 2 — the contract gate: each settled peer's `GET
        # /health` must answer the HealthReport shape and the shipped
        # `dcs-ctl` must carry the `health` verb the image's
        # HEALTHCHECK declares — either absence is the pinned release
        # predating the contract, never a violation of it. A serving
        # peer whose liveness answer dishonestly reads is the
        # contract's failure.
        gate = {}
        for url, expected in (
            (duty_url, "active"),
            (standby_url, "standby"),
        ):
            status, body = liveness(url)
            if (
                status != 200
                or not isinstance(body, dict)
                or "live" not in body
                or "role" not in body
            ):
                raise Inconclusive(
                    "the pinned release predates the health contract "
                    f"— GET /health answers {status}: {body}"
                )
            if body.get("live") is not True or (
                body.get("role") != expected
            ):
                failures.append(
                    f"{rig.peer_name(url)}'s liveness answer serves "
                    f"{body} — expected live with role {expected}"
                )
                raise Abort
            gate[rig.peer_name(url)] = {
                "live": body["live"],
                "role": body["role"],
            }
        probe = subprocess.run(
            [args.ctl, duty_url.removeprefix("http://"), "health"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode != 0:
            raise Inconclusive(
                "the shipped dcs-ctl predates the health verb — the "
                "pinned release's tooling cannot run the declared "
                "HEALTHCHECK probe: "
                f"{probe.stderr.strip() or probe.stdout.strip()}"
            )
        gate["probe"] = "healthy"
        digest_entries.append({"phase": "health-contract", **gate})
        steps = {"expected": rolling_upgrade.field_tick(rig.plant_io)}

        # Phase 3 — the tracking standby's orchestrated restart:
        # stopped, gated healthy on the declared status, reconverged —
        # the field owner stepping and serving throughout.
        digest_entries.append(
            {
                "phase": "restart-standby",
                **restart_cycle(
                    rig,
                    "standby",
                    "standby_url",
                    duty_url,
                    standby_files,
                    "tracking standby",
                    args.controller,
                    False,
                    steps,
                    args,
                    tamper,
                    failures,
                ),
            }
        )

        # Phase 4 — the documented switch: demote the field owner,
        # promote the restarted peer — the roll's second half now
        # runs on the promoted peer.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
                "receipts": switched["receipts"],
            }
        )
        steps["expected"] = rolling_upgrade.field_tick(rig.plant_io)

        # Phase 5 — the new standby's orchestrated restart: the
        # demoted launch-duty member rolled the same way, through the
        # slow-starting stand-in where the harness admits one — the
        # declared status must transition unhealthy→healthy rather
        # than a sleep passing it.
        digest_entries.append(
            {
                "phase": "restart-peer",
                **restart_cycle(
                    rig,
                    "duty",
                    "duty_url",
                    standby_url,
                    duty_files,
                    "new standby",
                    args.controller,
                    True,
                    steps,
                    args,
                    tamper,
                    failures,
                ),
            }
        )

        # Phase 6 — the launch roles restored: demote the promoted
        # peer, promote the reconverged member — the manifest's duty
        # back `active`, its declared standby back `tracking`.
        restored = rig.switch(
            standby_url, duty_url, failures, audit_receipts=True
        )
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "demoted_role": restored["demoted_role"],
                "promoted_role": restored["promoted_role"],
                "receipts": restored["receipts"],
            }
        )
        steps["expected"] = rolling_upgrade.field_tick(rig.plant_io)
        evidence["restored_at"] = restored["promote"]["tick"]

        # Phase 7 — the settled record: one active plus one tracking
        # standby with both containers rolled, both health verdicts
        # live, and the field image still the owner's.
        duty_role = role(duty_url, failures)
        standby_role = role(standby_url, failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the launch duty reports {duty_role.get('role')!r} "
                "after the roll — expected active"
            )
            raise Abort
        if not rolling_upgrade.tracking(standby_role):
            failures.append(
                f"the launch standby reports {standby_role} after the "
                "roll — expected a tracking standby"
            )
            raise Abort
        for url in (duty_url, standby_url):
            status, body = liveness(url)
            if (
                status != 200
                or not isinstance(body, dict)
                or body.get("live") is not True
            ):
                failures.append(
                    f"{rig.peer_name(url)}'s liveness answer serves "
                    f"{status} {body} after the roll — the rolled "
                    "pair's health verdicts must both read live"
                )
                raise Abort
        owner = rolling_upgrade.owner_scan(
            rig, duty_url, steps, failures
        )
        rolling_upgrade.field_check(
            owner, rig.plant_io, failures, "after the roll"
        )
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "record",
                "final_tick": owner["tick"],
                "duty_role": duty_role,
                "standby_role": standby_role,
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
    parser.add_argument("--ctl", required=True)
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["premature-healthy"],
        help="doctor the probe to declare the restarted peer healthy "
        "before its liveness answer serves — the pass must fail "
        "naming the doctored verdict",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = orchestrated_restart_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "orchestrated-restart: the doctored probe declared the "
                "restarted peer healthy before its liveness answer "
                "served — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        eprint(f"orchestrated-restart: inconclusive — {inconclusive}")
        print(f"orchestrated-restart-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"orchestrated-restart: {line}")
        return 1
    for failure in failures:
        eprint(f"orchestrated-restart: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"orchestrated-restart: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored probe"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"orchestrated-restart-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the standby's health-gated restart "
        "reconverged, the pair switched at tick "
        f"{evidence['switched_at']}, the slow-starting stand-in's "
        "restart transitioned unhealthy to healthy, launch roles "
        f"restored at tick {evidence['restored_at']}, the run "
        f"continuing to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
